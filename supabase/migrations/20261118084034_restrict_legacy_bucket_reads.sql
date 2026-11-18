BEGIN;
SET LOCAL lock_timeout = '3s';
LOCK TABLE storage.objects IN ACCESS EXCLUSIVE MODE;

-- Direct downloads and signing can bypass session revocation at the CDN.
-- Keep only the operations used by the corpus library, with the existing
-- owner and active-session restrictions still enforced by the other policies.
CREATE POLICY corpora_private_objects_operation_guard ON storage.objects
AS RESTRICTIVE FOR SELECT TO authenticated
USING (
    bucket_id <> 'project-corpora' OR storage.allow_any_operation(ARRAY[
        'object.upload', 'object.upload_update', 'object.delete',
        'object.delete_many', 'object.list', 'object.list_v2'
    ])
);
COMMIT;
